"""A money-map row says what is left of the same money the tiles hand out.

A ledger that begins inside the month may spend only the budget's share
from its first day to month end (budget.pace_window,
specs/short-ledger-pacing.md). The hero and the tiles already read that
share; a plan row that measured the whole month's budget would print a
bigger "$X left" beside a tile that hands out less, on the same page and
in the same email. So each judged row's budget is the paced share, and
what it says is left equals what the tile hands out from now to month
end: its daily rate over the days it covers, plus what is left of today
once today has spending (budget.daily_rate).
"""

import datetime as dt
import unittest

from oikonome.web import report, todayview

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 10, 16)           # day 16 of a 31-day October
FIRST = dt.date(2026, 10, 10)           # the ledger's first day
PETS = [{"name": "Pets", "parent": "other", "monthly": 100,
         "merchants": ["Chewy"]}]


def _household(custom=None):
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=900,
                 **({"custom_buckets": custom} if custom else {}))
    d = FIRST
    while d < TODAY:
        add_txn(conn, d, 25.0, "CORNER GROCER", primary="FOOD_AND_DRINK")
        add_txn(conn, d, 20.0, "HARDWARE BARN")
        d += dt.timedelta(days=1)
    # a little spent today, inside today's allowance
    add_txn(conn, TODAY, 5.0, "CORNER GROCER", primary="FOOD_AND_DRINK")
    add_txn(conn, TODAY, 5.0, "HARDWARE BARN")
    if custom:
        add_txn(conn, FIRST, 30.0, "Chewy", merchant="Chewy")
    return conn


class MoneyMapPacedRowsTests(unittest.TestCase):

    def _check(self, custom=None):
        conn = _household(custom)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        share = st["pace"]["share"]
        self.assertAlmostEqual(share, 22 / 31)
        _a, _k, tiles = todayview.allowances(st)
        tile = {label: a for label, a in tiles}
        rows = {r["label"]: r for r in todayview._plan_rows(st)
                if r["judged"]}
        self.assertEqual(set(rows), set(tile))
        for label, r in rows.items():
            with self.subTest(row=label):
                a = tile[label]
                handed_out = (a["rate"] * a["rate_days"]
                              + (a["left_today"] if a["spent_today"] > 0
                                 else 0.0))
                self.assertAlmostEqual(r["budget"] - r["actual"],
                                       handed_out, delta=0.05)
        mm = todayview.money_map(st)
        self.assertAlmostEqual(mm["hero"]["budget"],
                               st["variable_budget_paced"], delta=0.01)
        total = next((r for r in mm["rows"]
                      if r["label"] == "Variable spending"), None)
        if total is not None:
            self.assertAlmostEqual(total["budget"],
                                   st["variable_budget_paced"], delta=0.01)
        # the Bills row is an obligation on its own schedule, not paced
        bills = next(r for r in todayview._plan_rows(st)
                     if r["label"] == "Bills")
        self.assertEqual(bills["budget"],
                         st["buckets"]["fixed"]["month_budget"])

    def test_a_mid_month_ledgers_rows_match_its_tiles(self):
        self._check()

    def test_a_carve_out_and_its_parent_match_their_tiles(self):
        self._check(PETS)

    def test_a_ledger_from_before_the_month_keeps_the_whole_budget(self):
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=900)
            add_txn(conn, dt.date(2026, 9, 3), 25.0, "CORNER GROCER",
                    primary="FOOD_AND_DRINK")
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        food = next(r for r in todayview._plan_rows(st)
                    if r["label"] == "Food")
        self.assertEqual(food["budget"], 600)


if __name__ == "__main__":
    unittest.main()


class TodayPayloadCarriesPacedBudgetsTests(unittest.TestCase):
    """The web and the app draw the same rows from /api/today/full, so the
    payload names each bucket's paced budget beside its month budget."""

    def test_the_today_payload_names_each_buckets_paced_budget(self):
        import os
        import uuid
        from unittest import mock

        from fastapi.testclient import TestClient

        from oikonome.db import tenancy

        from .util import _ensure_db, seed_accounts
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        client = TestClient(appmod.app)
        client.post("/api/signup", data={
            "email": f"paced-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=620, other_monthly=930,
                         custom_buckets=PETS)
            add_txn(conn, FIRST, 25.0, "CORNER GROCER",
                    primary="FOOD_AND_DRINK")
        finally:
            conn.close()
        with mock.patch("oikonome.web.api._today", return_value=TODAY), \
                mock.patch("oikonome.localtime.now_local",
                           return_value=dt.datetime.combine(TODAY,
                                                            dt.time(12))):
            r = client.get("/api/today/full")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        share = 22 / 31
        self.assertAlmostEqual(d["month_share"], share, places=5)
        food, other = d["buckets"]["food"], d["buckets"]["other"]
        self.assertAlmostEqual(food["paced_budget"], 620 * share, delta=0.01)
        self.assertAlmostEqual(other["paced_budget"], 930 * share, delta=0.01)
        self.assertAlmostEqual(other["paced_remaining_budget"],
                               830 * share, delta=0.01)
        self.assertAlmostEqual(other["children"][0]["paced_budget"],
                               100 * share, delta=0.01)
