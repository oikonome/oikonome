"""An envelope's health is judged against its own period and its own
matches.

A 12-month envelope used once a year read "stale — missed its cycle" two
months after its charge, because every envelope was judged on this month
and the last. A category envelope (a pool its category's rows fill) read
"off-ledger" while those rows were filling it, because only merchant
matches were read."""

import datetime as dt
import unittest

from oikonome.engine import bills

from .util import TODAY, add_bill, add_txn, make_db, write_config


class EnvelopeHealthTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _health(self, payee):
        return bills.analyze_bills(self.conn, TODAY)[payee]["health"]

    def test_a_yearly_envelope_used_months_ago_is_not_stale(self):
        add_bill(self.conn, "Namehost", 120.0, bill_type="envelope",
                 frequency=None, interval=12)
        add_txn(self.conn, TODAY - dt.timedelta(days=100), 40.0, "NAMEHOST",
                primary="GENERAL_SERVICES")
        self.assertEqual(self._health("Namehost"), "ok")

    def test_a_yearly_envelope_unused_for_over_a_year_is_stale(self):
        add_bill(self.conn, "Namehost", 120.0, bill_type="envelope",
                 frequency=None, interval=12)
        add_txn(self.conn, TODAY - dt.timedelta(days=420), 40.0, "NAMEHOST",
                primary="GENERAL_SERVICES")
        self.assertEqual(self._health("Namehost"), "stale")

    def test_a_monthly_envelope_is_still_judged_on_two_months(self):
        add_bill(self.conn, "Bean Haus", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, TODAY - dt.timedelta(days=100), 40.0, "BEAN HAUS",
                primary="FOOD_AND_DRINK")
        self.assertEqual(self._health("Bean Haus"), "stale")

    def test_a_category_envelope_its_rows_fill_is_on_the_ledger(self):
        add_bill(self.conn, "Services", 300.0, bill_type="envelope",
                 frequency=None, interval=1, category="GENERAL_SERVICES",
                 match_category=True)
        add_txn(self.conn, TODAY, 50.0, "ACME PLUMBING", primary="GENERAL_SERVICES")
        self.assertEqual(self._health("Services"), "ok")


if __name__ == "__main__":
    unittest.main()
