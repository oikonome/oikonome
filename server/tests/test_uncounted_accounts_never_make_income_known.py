"""Income is known only where the rows the income total counts begin.

A hidden account, or the non-primary copy of a linked pair, contributes
nothing to the income total: its deposits are dropped on purpose. If its
rows still decided which months have KNOWN income, a household that hid an
old checking account full of payroll would see those months as "$0 came
in" against the card spend of the same months, a loss nobody made, on the
Transactions month header, the Cash Flow year rows and windows, and the
Month and Year lenses. Those months must read as unknown instead, exactly
as they do when the account's rows are not there at all.
"""

import datetime as dt
import unittest

from oikonome.engine import links, reporting
from oikonome.web import lenses

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 10, 9)


def _seed(conn, *, how: str) -> None:
    """An older checking account with a year of payroll in 2022, a card
    with spend in 2022 and through September 2026, and the live checking
    whose payroll starts in June 2026. `how` says why the older account
    does not count: 'hidden' by the user, or the 'linked' backup copy
    behind the live checking."""
    write_config(conn)
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, subtype, "
        "balance_current) VALUES ('old', 'it1', 'Old Checking', "
        "'depository', 'checking', 0)")
    for m in range(1, 13):
        add_txn(conn, dt.date(2022, m, 10), -4000.0, "PAYROLL",
                primary="INCOME", detailed="INCOME_WAGES", account="old")
        add_txn(conn, dt.date(2022, m, 12), 900.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
    for m in range(1, 10):
        add_txn(conn, dt.date(2026, m, 12), 900.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
    for m in range(6, 10):
        add_txn(conn, dt.date(2026, m, 10), -4000.0, "PAYROLL",
                primary="INCOME", detailed="INCOME_WAGES", account="chk")
    if how == "hidden":
        conn.execute("UPDATE accounts SET user_removed_at = now() "
                     "WHERE id = 'old'")
        links.set_shadow_scope(conn)
    else:
        links.create(conn, ["chk", "old"])
    conn.commit()


class _Base:
    how = ""

    def setUp(self):
        self.conn = make_db()
        _seed(self.conn, how=self.how)
        # guard: the older account is one the money math ignores
        self.assertIn("old", links.shadow_ids(self.conn))

    def tearDown(self):
        self.conn.close()

    def test_coverage_starts_where_the_counted_rows_do(self):
        self.assertEqual(reporting._income_coverage(self.conn),
                         ({"2026"}, "2026-06"))
        self.assertEqual(reporting._bank_coverage_years(self.conn), {"2026"})

    def test_the_month_header_reads_unknown_not_a_loss(self):
        for y, m in ((2022, 3), (2026, 2)):
            f = reporting.month_flows(self.conn, y, m)
            self.assertEqual(f["out"], 900.0)
            self.assertIsNone(f["in"], f"{y}-{m:02d} income")
            self.assertIsNone(f["net"], f"{y}-{m:02d} net")
        f = reporting.month_flows(self.conn, 2026, 7)
        self.assertEqual((f["in"], f["net"]), (4000.0, 3100.0))

    def test_cash_flow_years_before_the_feed_are_unknown(self):
        cf = reporting.compute_cashflow(self.conn, TODAY)
        rows = {r[0]: r for r in cf["savings_by_year"]}
        self.assertEqual(rows["2022"], ["2022", None, 10800.0, None, None])
        self.assertIsNone(rows["2026"][1], "2026 is covered from June only")
        self.assertIsNone(rows["2026"][3])
        self.assertNotIn("2022", cf["monthly_by_year"])

    def test_a_window_starts_at_the_first_counted_month(self):
        fb = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertTrue(fb["income_clipped"])
        self.assertEqual(fb["from"], "2026-06-01")
        self.assertEqual(fb["in"]["total"], 16000.0)
        self.assertEqual(fb["out"]["total"], 3600.0)
        self.assertEqual(fb["saved"], 12400.0)
        self.assertEqual(fb["rate"], 77.5)

    def test_the_lenses_read_the_same_months_as_unknown(self):
        y = lenses.year_summary(self.conn, 2022, today=TODAY)
        for c in y["cells"]:
            self.assertIsNone(c["income"], f"2022-{c['m']:02d} income")
            self.assertIsNone(c["net"], f"2022-{c['m']:02d} net")
        self.assertIsNone(y["totals"]["saved"])
        feb = lenses.month_summary(self.conn, 2026, 2, today=TODAY)
        self.assertIsNone(feb["income"]["actual"])
        jul = lenses.month_summary(self.conn, 2026, 7, today=TODAY)
        self.assertEqual(jul["income"]["actual"], 4000)


class HiddenAccountTests(_Base, unittest.TestCase):
    how = "hidden"


class LinkedBackupTests(_Base, unittest.TestCase):
    how = "linked"


if __name__ == "__main__":
    unittest.main()
