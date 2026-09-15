"""The spend-at-most card's today-ledger.

The /day rate amortizes today's spending over the whole rest of the month,
so buying lunch barely moves it and it never answers "can I spend more
TODAY?". The today-ledger does: each category's allowance is FIXED at the
start of the day (remaining at midnight ÷ days left) and today's spend
deducts from it in full. Negative = over for today; tomorrow's allowance
recomputes and absorbs it.
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

# util config: food_monthly=1000, other_monthly=1000. TODAY = 2026-07-15,
# July has 31 days -> days_left = 17.
DAYS_LEFT = 17


class TodayLedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _allow(self):
        st = budget.month_status(self.conn, TODAY)
        st["today"], st["days_in_month"] = TODAY, 31
        return todayview.allowances(st)

    def test_todays_spend_deducts_in_full(self):
        # $200 earlier in the month, then $30 today
        add_txn(self.conn, TODAY - dt.timedelta(days=4), 200.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 30.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        allow, _, _ = self._allow()
        f = allow["food"]
        # allowance was set before today's purchase: (1000-200)/17
        self.assertAlmostEqual(f["today_allowance"], 800 / DAYS_LEFT, 2)
        self.assertAlmostEqual(f["spent_today"], 30.0, 2)
        # ...and today's $30 comes off it dollar for dollar
        self.assertAlmostEqual(f["left_today"], 800 / DAYS_LEFT - 30, 2)
        # while the /day rate amortizes the same $30 over 17 days — the
        # difference is the entire point of the feature
        self.assertAlmostEqual(f["rate"], 770 / DAYS_LEFT, 2)
        # the tile meter's month scope: full track = month budget, fill =
        # month-to-date spend (the pace tick renders at today client-side)
        self.assertAlmostEqual(f["month_budget"], 1000.0, 2)
        self.assertAlmostEqual(f["month_spent"], 230.0, 2)

    def test_envelope_overflow_from_an_earlier_day_is_not_spent_today(self):
        """An envelope swipe that blew past its cap ten days ago overflows
        to variable on the day it HAPPENED. The overflow must not be
        stamped today, or the card says a day's budget was already spent
        this morning on a purchase nobody made today."""
        add_bill(self.conn, "Corner Coffee", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, TODAY - dt.timedelta(days=10), 150.0, "CORNER COFFEE",
                primary="FOOD_AND_DRINK")
        st = budget.month_status(self.conn, TODAY)
        # the $50 over the cap is this month's food spend...
        self.assertAlmostEqual(st["buckets"]["food"]["actual"], 50.0, 2)
        allow, _, _ = self._allow()
        # ...but none of it was spent today
        self.assertAlmostEqual(allow["food"]["spent_today"], 0.0, 2)
        self.assertAlmostEqual(allow["food"]["left_today"],
                               allow["food"]["today_allowance"], 2)

    def test_overspending_today_goes_negative(self):
        add_txn(self.conn, TODAY, 500.0, "TARGET")
        o = self._allow()[0]["other"]
        self.assertAlmostEqual(o["today_allowance"], 1000 / DAYS_LEFT, 2)
        self.assertAlmostEqual(o["left_today"], 1000 / DAYS_LEFT - 500, 2)
        self.assertLess(o["left_today"], 0)

    def test_a_bare_refund_does_not_count(self):
        """Money conventions: spend rows are amount > 0 only — a raw
        credit is invisible to the budget math, and money comes back only
        through EXPLICIT reimbursement pairing (which nets the expense row
        via NET_AMOUNT). The today-ledger inherits that, deliberately."""
        add_txn(self.conn, TODAY, 40.0, "TARGET")
        add_txn(self.conn, TODAY, -15.0, "TARGET REFUND")
        o = self._allow()[0]["other"]
        self.assertAlmostEqual(o["spent_today"], 40.0, 2)
        self.assertAlmostEqual(o["left_today"], 1000 / DAYS_LEFT - 40, 2)

    def test_custom_bucket_gets_its_own_tile_and_parent_excludes_it(self):
        write_config(self.conn, custom_buckets=[
            {"name": "Fun", "parent": "other", "monthly": 340,
             "categories": ["ENTERTAINMENT"], "merchants": []}])
        add_txn(self.conn, TODAY, 20.0, "CINEMA", primary="ENTERTAINMENT")
        add_txn(self.conn, TODAY, 50.0, "TARGET")
        allow, kids, tiles = self._allow()
        self.assertEqual([k["name"] for k in kids], ["Fun"])
        fun = kids[0]
        self.assertAlmostEqual(fun["today_allowance"], 340 / DAYS_LEFT, 2)
        self.assertAlmostEqual(fun["left_today"], 340 / DAYS_LEFT - 20, 2)
        # the parent tile carries the carved numbers: budget 1000-340,
        # and the cinema $20 belongs to Fun, not to Everything else
        o = allow["other"]
        self.assertAlmostEqual(o["spent_today"], 50.0, 2)
        self.assertAlmostEqual(o["today_allowance"], 660 / DAYS_LEFT, 2)
        self.assertEqual([t[0] for t in tiles],
                         ["Food", "Everything else", "Fun"])

    def test_budget_already_gone_floors_the_allowance(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=4), 1200.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 10.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        f = self._allow()[0]["food"]
        self.assertEqual(f["today_allowance"], 0.0)
        self.assertAlmostEqual(f["left_today"], -10.0, 2)

    def test_email_mirrors_the_ledger(self):
        """Standing rule: every Today-page element lands in the email HTML
        and the plain text in the same change. The ledger lives in the
        VERDICT pane/block."""
        add_txn(self.conn, TODAY, 30.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        d = report.gather(self.conn, TODAY)
        _, plain, html = report.build(d)
        self.assertIn("Left to spend today", html)
        # one tile line — left to spend today against the day's fixed
        # allowance
        self.assertIn(">> LEFT TO SPEND TODAY: ", plain)
        line = next(ln for ln in plain.splitlines()
                    if ln.startswith(">> LEFT TO SPEND TODAY"))
        self.assertIn("Food", line)
        # ...directly under the hero verdict sentence
        idx = plain.splitlines().index(line)
        self.assertIn("pace", plain.splitlines()[idx - 1])


if __name__ == "__main__":
    unittest.main()
