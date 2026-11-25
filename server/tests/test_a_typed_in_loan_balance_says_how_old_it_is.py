"""A mortgage balance typed in by hand says when it was entered and, a
quarter on, asks to be updated (specs/counted-figures.md, "A typed-in loan
balance says how old it is"). It was subtracted at the figure typed months
earlier under "figures as of" today, with nothing to say it was old.
"""
import datetime as dt
import pathlib
import unittest
from unittest import mock

from oikonome.engine import reporting

from .util import make_db, write_config

TODAY = dt.date(2026, 11, 17)
_ROOT = pathlib.Path(__file__).resolve().parents[2]


class TypedInLoanSaysItsAge(unittest.TestCase):
    def _items(self, assets, loans=()):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, manual_assets=assets)
        for i, bal in enumerate(loans):
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES (%s,'it1','Home Loan','loan',"
                "'mortgage',%s)", (f"mtg{i}", bal))
        with mock.patch.object(reporting, "_household_today",
                               return_value=TODAY):
            nw = reporting.compute_networth(conn, today=TODAY, trend=False)
        return nw, {p["name"]: p for p in nw["property_items"]}

    def test_a_balance_stamped_with_the_month_alone_still_says_its_age(self):
        # older rows carry "YYYY-MM"; without this they said nothing at all
        _, items = self._items([
            {"name": "Home loan", "kind": "mortgage", "value": -200000,
             "as_of": "2026-06"}])
        self.assertEqual(items["Home loan"]["entered_text"],
                         "entered 06/26 — update")
        self.assertTrue(items["Home loan"]["update_due"])

    def test_a_balance_a_quarter_old_asks_for_an_update(self):
        nw, items = self._items([
            {"name": "Home", "kind": "real_estate", "value": 300000,
             "as_of": "2026-06-04"},
            {"name": "Home loan", "kind": "mortgage", "value": -200000,
             "as_of": "2026-06-04"}])
        loan = items["Home loan"]
        self.assertEqual(loan["entered_text"], "entered 06/26 — update")
        self.assertTrue(loan["update_due"])
        self.assertEqual(nw["mortgage_entered_text"],
                         "entered 06/26 — update")
        # the house is a value, not a balance being paid down
        self.assertIsNone(items["Home"]["entered_text"])
        self.assertFalse(items["Home"]["update_due"])

    def test_a_recent_balance_only_says_when(self):
        _nw, items = self._items([
            {"name": "Home loan", "kind": "mortgage", "value": -200000,
             "as_of": "2026-09-30"}])
        self.assertEqual(items["Home loan"]["entered_text"], "entered 09/26")
        self.assertFalse(items["Home loan"]["update_due"])

    def test_a_row_with_no_readable_date_says_nothing(self):
        nw, items = self._items([
            {"name": "Home loan", "kind": "mortgage", "value": -200000}])
        self.assertIsNone(items["Home loan"]["entered_text"])
        self.assertFalse(items["Home loan"]["update_due"])
        self.assertIsNone(nw["mortgage_entered_text"])

    def test_a_linked_mortgage_is_the_lenders_balance(self):
        nw, items = self._items([
            {"name": "Home loan", "kind": "mortgage", "value": -200000,
             "as_of": "2026-01-02"}], loans=(170000,))
        self.assertNotIn("Home loan", items)
        self.assertIsNone(nw["mortgage_entered_text"])
        for p in nw["property_items"]:
            self.assertIsNone(p["entered_text"])

    def test_both_clients_print_the_servers_words(self):
        web = (_ROOT / "webapp/src/pages/NetWorth.tsx").read_text()
        mob = (_ROOT / "mobile/src/app/networth.tsx").read_text()
        for src, who in ((web, "web"), (mob, "phone")):
            self.assertTrue("p.entered_text" in src, who)
            self.assertTrue("mortgage_entered_text" in src, who)


if __name__ == "__main__":
    unittest.main()
