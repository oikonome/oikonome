"""Today's money map is a this-month card: the Savings / Investment row
reads "transferred vs plan", so its actual is what was transferred toward
the plan THIS month — the same posted figure the Month lens shows — not
the trailing 90-day average. The average stays in the payload for the
goal's pace, under its own name, and older clients that read it keep
working.

Worked case: plan $500/mo, one $1,500 transfer two and a half months ago
and nothing since. The 90-day average reads $500 ("on plan"); this month
nothing was transferred.
"""

import datetime as dt
import unittest

from oikonome.web import lenses, report, todayview

from .util import TODAY, add_ledger_history, add_txn, make_db, write_config


class TodaySavingsRowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, savings_goals=[
            {"name": "Savings", "monthly_plan": 500.0, "account_id": "sav",
             "tokens": [], "start_balance": 0}])
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0) ON CONFLICT (tenant_id, id) DO NOTHING")
        # the account has a ledger older than the 90-day window, and one
        # transfer inside the window, none this month
        add_ledger_history(self.conn, TODAY, account="sav")
        add_txn(self.conn, TODAY - dt.timedelta(days=75), -1500.0,
                "TO SAVINGS", account="sav", primary="TRANSFER_IN")

    def tearDown(self):
        self.conn.close()

    def _goal(self, d):
        return next(g for g in d["savings_goals"] if g["name"] == "Savings")

    def _row(self, d):
        return next(r for r in todayview._plan_rows(d)
                    if r["label"] == "Savings / Investment")

    def test_the_payload_carries_this_months_transfer_beside_the_average(self):
        d = report.gather(self.conn, TODAY)
        g = self._goal(d)
        self.assertEqual(g["rate_90d"], 500.0)        # unchanged for pace
        self.assertEqual(g["saved_this_month"], 0.0)

    def test_the_row_reads_this_months_transfer(self):
        d = report.gather(self.conn, TODAY)
        row = self._row(d)
        self.assertEqual(row["actual"], 0.0)
        self.assertEqual(row["budget"], 500.0)
        self.assertEqual(row["caption"], "transferred vs plan")

    def test_a_transfer_this_month_counts_in_full(self):
        """A month's own $1,500 transfer reads $1,500, not the $500 the
        three-month average spreads it to."""
        self.conn.execute("DELETE FROM transactions WHERE account_id='sav'")
        add_txn(self.conn, TODAY.replace(day=2), -1500.0, "TO SAVINGS",
                account="sav", primary="TRANSFER_IN")
        d = report.gather(self.conn, TODAY)
        self.assertEqual(self._goal(d)["saved_this_month"], 1500.0)
        self.assertEqual(self._row(d)["actual"], 1500.0)

    def test_it_is_the_month_lens_figure(self):
        """Today and the Month lens describe the same month, so they agree."""
        for when in (TODAY.replace(day=2), TODAY - dt.timedelta(days=40)):
            add_txn(self.conn, when, -250.0, "TO SAVINGS",
                    account="sav", primary="TRANSFER_IN")
        d = report.gather(self.conn, TODAY)
        m = lenses.month_summary(self.conn, TODAY.year, TODAY.month,
                                 today=TODAY)
        self.assertEqual(self._goal(d)["saved_this_month"], m["saved_month"])
        self.assertEqual(m["saved_month"], 250.0)

    def test_the_email_row_reads_this_months_transfer(self):
        d = report.gather(self.conn, TODAY)
        _, _, html = report.build(d)
        import re
        i = html.index("Savings / Investment")
        words = " ".join(re.sub(r"<[^>]+>", " ", html[i:i + 3000]).split())
        self.assertIn("transferred vs plan", words)
        # $0 transferred this month against the $500 plan — not the
        # average's "$500 / $500"
        self.assertIn("Savings / Investment $0 / $500", words)

    def test_a_payload_without_the_new_field_falls_back_to_the_average(self):
        """An older payload (or a goal measured before the field existed)
        still renders a row rather than failing."""
        d = report.gather(self.conn, TODAY)
        for g in d["savings_goals"]:
            g.pop("saved_this_month", None)
        self.assertEqual(self._row(d)["actual"], 500.0)


if __name__ == "__main__":
    unittest.main()
