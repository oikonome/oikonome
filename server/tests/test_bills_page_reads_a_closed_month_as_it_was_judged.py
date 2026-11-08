"""The Bills page reads a closed month under the schedule and settings the
month was judged under.

`/api/bills?month=YYYY-MM` for a month that is over describes how that
month ended — the same reading the Month lens gives. The lens judges a
closed month against its snapshot (budget.snapshot_config and
budget.snapshot_bill_rows), so a setting or a bill changed afterwards
cannot rewrite it. The Bills page must read the same snapshot: otherwise
archiving a bill, excluding the account that paid it, or disabling it
today turns a rent the lens calls paid into "0 of 1" on the Bills page,
and one closed month gives two answers. With no snapshot the live
schedule applies on both.

The same holds for a closed month's paychecks: a check due on the month's
last day that posts two days later is that month's income on the lens, so
it is received on the Bills page too.
"""
import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import lenses
from oikonome.web.api import _month_bill_stats

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 20)


class _Case(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=600)
        add_bill(self.conn, "Harbor Lofts", 1500.0,
                 next_due=dt.date(2026, 9, 3),
                 last_seen=dt.date(2026, 8, 3), show_today=True)
        add_txn(self.conn, dt.date(2026, 9, 3), 1500.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def freeze_september(self):
        self.assertTrue(budget.snapshot_month(self.conn, dt.date(2026, 9, 30)))

    def september(self):
        st = lenses.month_state(self.conn, 2026, 9, TODAY)
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 st["as_of"], closed=True)
        return st, page

    def assert_rent_agrees(self):
        st, page = self.september()
        pin = next(p for p in st["pinned_bills"]
                   if p["payee"] == "Harbor Lofts")
        row = page.get("Harbor Lofts") or {}
        self.assertEqual((pin["occ_count"], pin["paid_count"]), (1, 1))
        self.assertEqual((row.get("occurrences"), row.get("paid")), (1, 1))
        self.assertAlmostEqual(row["paid_amount"], 1500.0, places=2)
        total = round(sum(e["paid_amount"] for e in page.values()), 2)
        self.assertAlmostEqual(total, st["buckets"]["fixed"]["actual"],
                               places=2)
        return st, page


class ABillArchivedAfterTheMonth(_Case):
    def test_the_closed_month_still_shows_it_paid(self):
        self.freeze_september()
        self.conn.execute(
            "UPDATE bills SET active=0 WHERE payee='Harbor Lofts'")
        add_bill(self.conn, "Harbor Lofts II", 1500.0,
                 next_due=dt.date(2026, 10, 3), last_seen=None)
        self.assert_rent_agrees()


class ThePageListsTheMonthAsItWasJudged(_Case):
    """The rows, not only the stats: a bill archived since still has its
    row in the closed month, and the disabled flag is the snapshot's."""

    def page_rows(self):
        from oikonome.web import api
        self.conn.commit()
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        out = api.bills_list(user={"tenant_id": tid, "user_id": "u",
                                   "role": "owner",
                                   "email": "o@example.com"},
                             month="2026-09")
        return {r["payee"]: r for r in out["bills"]}

    def test_an_archived_rent_keeps_its_row(self):
        self.freeze_september()
        self.conn.execute(
            "UPDATE bills SET active=0 WHERE payee='Harbor Lofts'")
        rows = self.page_rows()
        self.assertIn("Harbor Lofts", rows)
        self.assertEqual(rows["Harbor Lofts"].get("paid"), 1)

    def test_disabled_since_reads_as_it_was(self):
        self.freeze_september()
        write_config(self.conn, food_monthly=600, other_monthly=600,
                     disabled_bills=["Harbor Lofts"])
        self.assertFalse(self.page_rows()["Harbor Lofts"]["disabled"])


class ABillRepricedAfterTheMonth(_Case):
    def test_the_closed_month_matches_at_the_old_price(self):
        self.freeze_september()
        self.conn.execute(
            "UPDATE bills SET amount=-2600 WHERE payee='Harbor Lofts'")
        self.assert_rent_agrees()


class TheAccountExcludedAfterTheMonth(_Case):
    def test_the_closed_month_still_sees_the_payment(self):
        self.freeze_september()
        write_config(self.conn, food_monthly=600, other_monthly=600,
                     excluded_accounts=["chk"])
        self.assert_rent_agrees()


class ABillDisabledAfterTheMonth(_Case):
    """Disabled today, the rent is out of the budget set, so its payment
    would fall to the envelope that category-matches it — on the live
    reading. September was judged with the rent in the budget."""

    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Utilities", 400.0, bill_type="envelope",
                 frequency=None, interval=1, category="RENT_AND_UTILITIES",
                 match_category=True)
        self.conn.commit()

    def test_the_envelope_does_not_take_the_rent(self):
        self.freeze_september()
        write_config(self.conn, food_monthly=600, other_monthly=600,
                     disabled_bills=["Harbor Lofts"])
        st, page = self.assert_rent_agrees()
        env = next(e for e in st["buckets"]["fixed"]["envelopes"]
                   if e["payee"] == "Utilities")
        self.assertAlmostEqual(page["Utilities"]["used"], env["used"],
                               places=2)
        self.assertAlmostEqual(page["Utilities"]["used"], 0.0, places=2)


class NoSnapshotReadsTheLiveSchedule(_Case):
    def test_without_a_snapshot_both_read_live(self):
        self.assert_rent_agrees()


class APaycheckPostedAfterTheMonthEnded(unittest.TestCase):
    """Due Saturday October 31, posted Monday November 2: October's."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=600)
        add_bill(self.conn, "Northwind Mfg", 3200.0, income=True,
                 next_due=dt.date(2026, 11, 30),
                 last_seen=dt.date(2026, 9, 30))
        add_txn(self.conn, dt.date(2026, 9, 30), -3200.0,
                "NORTHWIND MFG PAYROLL", account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 11, 2), -3200.0,
                "NORTHWIND MFG PAYROLL", account="chk", primary="INCOME")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_october_received_it_on_both_pages(self):
        st = lenses.month_state(self.conn, 2026, 10, TODAY)
        self.assertAlmostEqual(st["income_received"], 3200.0, places=2)
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 st["as_of"], closed=True)
        row = page["Northwind Mfg"]
        self.assertEqual((row["occurrences"], row["paid"]), (1, 1))
        self.assertAlmostEqual(row["paid_amount"], 3200.0, places=2)

    def test_november_does_not_count_octobers_check(self):
        page = _month_bill_stats(self.conn, budget.load_config(self.conn),
                                 TODAY)
        row = page["Northwind Mfg"]
        self.assertEqual(row["paid"], 0)


if __name__ == "__main__":
    unittest.main()
