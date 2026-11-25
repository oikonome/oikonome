"""A yearly envelope's row on the Month lens reads "spent this year of the
year's pool", never this month's swipes over the pool's monthly share.

A $96-a-year envelope carries $8 a month in the plan; a $33 renewal in
July printed as "$33 of $8", which reads as more than three times over a
cap it is well inside. The server decides the pair (budget.envelope_shown):
a yearly pool prints the year's spend so far of the year's pool, a monthly
one its month of its month. Both clients print the two figures as sent.
"""
import pathlib
import unittest

from oikonome.engine import budget
from oikonome.web import lenses

from .util import TODAY, add_bill, add_txn, make_db, write_config

ROOT = pathlib.Path(__file__).resolve().parents[2]


class TheServerPicksThePair(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Quillfeather Hosting", 96.0, bill_type="envelope",
                 frequency=None, interval=12)
        add_bill(self.conn, "Bramblewick Beans", 60.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, TODAY.replace(month=3, day=4), 14.0,
                "QUILLFEATHER HOSTING", primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY.replace(day=2), 33.0,
                "QUILLFEATHER HOSTING", primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY.replace(day=3), 22.0,
                "BRAMBLEWICK BEANS", primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def test_the_month_lens_sends_the_years_spend_of_the_pool(self):
        r = lenses.month_summary(self.conn, TODAY.year, TODAY.month,
                                 today=TODAY)
        env = {e["payee"]: e for e in r["bills"]["envelopes"]}
        yearly = env["Quillfeather Hosting"]
        self.assertEqual((yearly["shown_used"], yearly["shown_of"]),
                         (47.0, 96.0))
        monthly = env["Bramblewick Beans"]
        self.assertEqual((monthly["shown_used"], monthly["shown_of"]),
                         (22.0, 60.0))

    def test_the_rule_reads_the_pool_length(self):
        self.assertEqual(budget.envelope_shown(
            {"used": 33.0, "monthly": 8.0, "pool": 96.0,
             "period_months": 12, "period_used": 47.0}), (47.0, 96.0))
        self.assertEqual(budget.envelope_shown(
            {"used": 22.0, "monthly": 60.0, "pool": 60.0,
             "period_months": 1, "period_used": 22.0}), (22.0, 60.0))


class BothClientsPrintThePairAsSent(unittest.TestCase):
    def test_the_web_prints_the_shown_pair(self):
        web = (ROOT / "webapp/src/pages/MonthLens.tsx").read_text()
        self.assertIn("{money(e.shown_used)} of {money(e.shown_of)}", web)
        self.assertNotIn("{money(e.used)} of {money(e.monthly)}", web)

    def test_the_phone_prints_it_when_sent(self):
        phone = (ROOT / "mobile/src/app/month.tsx").read_text()
        self.assertIn("e.shown_used !== undefined", phone)
        self.assertIn("older-server path: stands in for `shown_used` / `shown_of`",
                      phone)


if __name__ == "__main__":
    unittest.main()
