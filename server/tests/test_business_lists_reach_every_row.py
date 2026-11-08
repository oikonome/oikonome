"""Business lists that carry a per-row control reach every row.

A list cut to its first N rows with no way to see the rest makes rows N+1
onward unreachable: the 1099 panel cut to 12 payees left a contractor
ranked 13th impossible to mark, so the business was never told it owed
that contractor a 1099-NEC; the mileage log cut to 6 trips left trip 7 on
undeletable. Both clients must open the whole list, and the 1099 panel
must by default show every payee at or above the reporting line (and any
already marked), so the ones that matter are never behind the toggle.

The 1099 door also takes the payee's TIN last four; both clients send it,
and a contractor toggle carries the TIN already on file, since the door
stores what it is sent and an omitted TIN would clear it.
"""

import pathlib
import re
import unittest

import oikonome

ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent
WEB = "webapp/src/pages/Business.tsx"
MOBILE = "mobile/src/app/business.tsx"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


def _between(src: str, start: str, end: str, rel: str) -> str:
    m = re.search(re.escape(start) + r".*?" + re.escape(end), src, re.S)
    if m is None:
        raise AssertionError(f"{rel}: {start!r} .. {end!r} not found")
    return m.group(0)


class BusinessListsReachEveryRow(unittest.TestCase):
    def setUp(self):
        try:
            self.web = _read(WEB)
            self.mobile = _read(MOBILE)
        except FileNotFoundError:
            self.skipTest("clients not present")
        self.web_vendors = _between(self.web, "function VendorSection",
                                    "function ComplianceSection", WEB)
        self.web_mileage = _between(self.web, "function MileageSection",
                                    "function BalanceSheetSection", WEB)
        self.mob_vendors = _between(self.mobile, "<H>1099 contractors",
                                    "<H>Compliance calendar</H>", MOBILE)
        self.mob_mileage = _between(self.mobile, "<H>Mileage log</H>",
                                    "</Card>", MOBILE)
        # the phone's Tax tab holds the vendor rule above its JSX
        self.mob_tax = _between(self.mobile, "function Tax(",
                                "<H>Compliance calendar</H>", MOBILE)

    def test_vendor_list_opens_every_payee(self):
        for rel, sec in ((WEB, self.web_vendors), (MOBILE, self.mob_vendors)):
            self.assertNotRegex(sec, r"\.slice\(0,\s*12\)", rel)
            self.assertIn("Show all (", sec, rel)

    def test_vendor_list_defaults_to_payees_at_the_threshold(self):
        """The rows shown before the toggle are every payee paid at or
        above the line plus every one already marked, never a fixed
        count."""
        for rel, sec in ((WEB, self.web_vendors), (MOBILE, self.mob_tax)):
            self.assertRegex(sec, r"paid\s*>=\s*\w+", rel)
            self.assertRegex(sec, r"\.reportable\s*\|\|", rel)

    def test_mileage_log_opens_every_trip(self):
        for rel, sec in ((WEB, self.web_mileage), (MOBILE, self.mob_mileage)):
            self.assertIn("Show all (", sec, rel)
            self.assertNotRegex(sec, r"trips[^\n]*\)\.slice\(0,\s*6\)\.map",
                                rel)

    def test_both_clients_send_the_tin_last_four(self):
        for rel in ("webapp/src/api/client.ts", "mobile/src/lib/api.ts"):
            call = re.search(r"vendorMark:.*?=>", _read(rel), re.S)
            self.assertIsNotNone(call, rel)
            self.assertIn("tin_last4", call.group(0), rel)
        for rel, sec in ((WEB, self.web_vendors), (MOBILE, self.mob_vendors)):
            self.assertRegex(sec, r"TIN", rel)

    def test_contractor_toggle_leaves_the_tin_to_the_server(self):
        """A toggle never sends a cached TIN back (it could undo a TIN
        saved meanwhile); the door keeps the one on file when none is sent."""
        for rel, sec in ((WEB, self.web_vendors), (MOBILE, self.mob_tax)):
            self.assertNotRegex(sec, r"tin_last4\s*:\s*v\.tin_last4", rel)
            self.assertIn("tin_last4 === undefined", sec, rel)

    def test_a_pasted_full_tin_keeps_its_last_four(self):
        for rel, sec in ((WEB, self.web_vendors), (MOBILE, self.mob_tax)):
            self.assertIn('.replace(/\\D/g, "").slice(-4)', sec, rel)

class AToggleKeepsTheTinOnFile(unittest.TestCase):
    def test_a_mark_sent_without_the_tin_keeps_it(self):
        """The contractor toggle may come from a client that does not send
        the TIN; only an explicit blank clears the one on file."""
        from oikonome.engine import entities, selfemploy

        from .util import make_db
        conn = make_db()
        self.addCleanup(conn.close)
        e = entities.create_entity(conn, name="Invented Studio LLC",
                                   structure="single_member_llc")
        selfemploy.mark_vendor(conn, e["id"], "Invented Contractor",
                               reportable=True, tin_last4="1234")
        self.assertEqual(selfemploy.vendor_tin(conn, e["id"],
                                               "Invented Contractor"), "1234")
        selfemploy.mark_vendor(conn, e["id"], "Invented Contractor",
                               reportable=False,
                               tin_last4=selfemploy.vendor_tin(
                                   conn, e["id"], "Invented Contractor"))
        self.assertEqual(selfemploy.vendor_tin(conn, e["id"],
                                               "Invented Contractor"), "1234")


if __name__ == "__main__":
    unittest.main()
