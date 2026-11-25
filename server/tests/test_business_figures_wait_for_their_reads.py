"""The Business screens never read a failed read as "nothing".

A failed review-queue read showed "Needs attention 0 — nothing
outstanding" and "Nothing to review — every business transaction has a
line."; a failed compliance read showed "Next deadline: none" and "No
obligations tracked", which hides tax deadlines; failed 1099 payees read
"No business payees yet"; a failed P&L, transaction list or tax estimate
left the header and nothing else; failed accounts read "No accounts yet".
Each figure now comes through knownOr (the unknown mark "·" when there is
none, greyed when kept), and each failed read keeps its section's place
with the failed line (specs/screen-states.md).
"""

import pathlib
import re
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


def _fn(src: str, head: str) -> str:
    body = src[src.index(head):]
    return body[:body.index("\n}\n")]


class BusinessFiguresWait(unittest.TestCase):
    def test_the_overview_counts_only_from_reads_that_answered(self):
        for p, head, cal, queue in (
                (_WEB / "pages" / "Business.tsx",
                 "function OverviewSection(", "comp", "sugg"),
                (_MOB / "app" / "business.tsx",
                 "function Overview(", "compliance", "sugg")):
            body = _fn(_src(self, p), head)
            self.assertNotRegex(body, r"unclassified \?\? 0", p)
            self.assertNotRegex(body, rf"\(\s*{cal}\.data\?\.obligations", p)
            self.assertIn(f"knownOr({cal}, (d) => d.obligations", body, p)
            self.assertIn(f"knownOr({queue}, (d) =>", body, p)
            self.assertIn("deadlines === null ? UNKNOWN_FIGURE", body, p)
            self.assertNotIn('"…"', body, p)
            for q, what in (("pnl", "this year's profit and cash"),
                            (cal, "the filing deadlines"),
                            (queue, "the review queue")):
                self.assertIn(f'<FailedLine q={{{q}}} what="{what}" />', body, p)
            self.assertIn("figureGreyed(", body, p)

    def test_each_section_keeps_its_place_when_its_read_failed(self):
        want = {
            _WEB / "pages" / "Business.tsx": [
                ("tax", "the tax set-aside"), ("q", "the review queue"),
                ("pnl", "the profit and loss"),
                ("listQ", "the business transactions"),
                ("q", "the 1099 payees"), ("q", "the filing deadlines"),
                ("accts", "the accounts"), ("sum", "your businesses")],
            _MOB / "app" / "business.tsx": [
                ("est", "the tax set-aside"), ("sugg", "the review queue"),
                ("pnl", "the profit and loss"),
                ("vendors", "the 1099 payees"),
                ("compliance", "the filing deadlines"),
                ("accounts", "the accounts"),
                ("summary", "your businesses")],
            _WEB / "components" / "BusinessWizard.tsx": [
                ("accounts", "your accounts")],
            _MOB / "app" / "business-wizard.tsx": [
                ("accounts", "your accounts")],
        }
        for p, lines in want.items():
            src = _src(self, p)
            for q, what in lines:
                self.assertIn(f'<FailedLine q={{{q}}} what="{what}" />', src,
                              (p, what))
            # the raw "Couldn't load: Error: 500: {…}" text is gone
            self.assertNotRegex(src, r"\{String\(\w+\.error\)\}", p)

    def test_the_empty_lines_wait_for_an_answer(self):
        web = _src(self, _WEB / "pages" / "Business.tsx")
        self.assertIn('{q.data !== undefined && all.length === 0', web)
        self.assertIn("if (q.data === undefined)\n    return (", web)
        tax = _fn(web, "function TaxSetAside(")
        self.assertNotIn("if (!tax.data) return null;", tax)
        mob = _src(self, _MOB / "app" / "business.tsx")
        self.assertIn("vendors.data !== undefined && vendors.data.vendors"
                      ".length === 0", mob)
        self.assertNotIn("(compliance.data?.obligations ?? []).length === 0",
                         mob)
        # the phone's figures print the one unknown mark, never a dash
        self.assertNotRegex(mob, re.escape('n == null ? "—" : money(n, false)'))


if __name__ == "__main__":
    unittest.main()
