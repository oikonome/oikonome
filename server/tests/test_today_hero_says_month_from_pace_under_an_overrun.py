"""Today's hero under an overrun prints two over-figures — the pace's (more
than planned by today) and the month's (over the month's budget) — and a
"left to spend today" that is a bucket not yet over. Said without words
for which is which, a household read two figures for one thing and a
spendable amount under a month already spent. The server words each: the
month figure names the month, the label under a spent month says the
amount is from the budgets not yet over. The email renders the same
words as the page. Figures are invented."""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import report

from .util import _ensure_db, add_txn, seed_accounts, write_config


class OverrunHeroWords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"overrun-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=900, other_monthly=300)
            today = dt.date.today()
            # everything else blows the whole month; food is untouched
            add_txn(conn, today.replace(day=1), 2500.0, "PINE OUTFITTERS")
        finally:
            conn.close()

    def test_the_month_figure_names_the_month_and_the_label_the_buckets(self):
        d = self.client.get("/api/today/full").json()
        self.assertLess(d["month_remaining_paced"], 0)
        self.assertGreater(d["simple"]["left_today"], 0)
        self.assertTrue(d["month_words"].startswith("over the month's "),
                        d["month_words"])
        self.assertIn("day", d["month_words"])
        self.assertEqual(d["left_label"],
                         "Left to spend today, in budgets not yet over")

    def test_the_email_says_the_same_words(self):
        d = self.client.get("/api/today/full").json()
        conn = tenancy.tenant_connect(self.tid)
        try:
            from oikonome.web.data import household_today
            st = report.gather(conn, household_today(conn))
            _subject, plain, html = report.build(st)[:3]
        finally:
            conn.close()
        self.assertIn(d["month_words"], plain)
        self.assertIn(d["left_label"].upper(), plain)
        self.assertIn(d["month_words"].replace("'", "&#39;"), html)
        self.assertIn(d["left_label"], html)


if __name__ == "__main__":
    unittest.main()
